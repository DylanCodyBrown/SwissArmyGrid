"""Server lifecycle: one instance, a port that sticks, ready before announced, clean exit.

The connection file (~/.swissarmygrid/server.json) is the contract between the server and its
clients, so this module keeps it truthful:

- One server at a time. Each server holds an OS lock named for the connection file for its whole
  life (`InstanceLock`), so two started at the same moment (VS Code restoring several windows,
  each starting the extension) can't both run. Then, before binding, read the file. If the
  process it names is alive and answers /health with its token, a server is already running: say
  where, and do not start a second one. If not, the file is left over from a crash and is replaced.
- Port choice. `--port N` means exactly N; a busy or invalid port is a plain error, never a
  silent switch. With no --port, reuse the last port if it is free (so bookmarks keep their
  address across restarts) and otherwise let the operating system pick a free one.
- No port sharing on Windows. Windows lets a second socket bind a port another process holds
  unless the first one asks for SO_EXCLUSIVEADDRUSE; without it, another program could bind
  127.0.0.1:<port> and receive requests (and tokens) meant for us.
- Ready before announced. The socket is listening before the file is written, so a client that
  finds the file can connect at once (early connections wait in the listen backlog).
- Clean exit. The file is deleted on Ctrl+C, on `swissarmygrid stop` (POST /shutdown), on a
  normal exit, and when the Windows console window is closed.
"""

from __future__ import annotations

import atexit
import ctypes
import errno
import hashlib
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

_LOCALHOST: str = "127.0.0.1"


class PortError(RuntimeError):
    """The requested port cannot be used. The message says why and what to do."""


class AlreadyRunningError(RuntimeError):
    """A healthy server already owns the connection file."""

    def __init__(self, info: dict[str, Any]) -> None:
        super().__init__(
            f"SwissArmyGrid is already running on http://127.0.0.1:{info.get('port')} "
            f"(process {info.get('pid')}). Restart it from VS Code's status bar, or stop it with "
            "`swissarmygrid stop` first."
        )
        self.info = info


class AlreadyStartingError(RuntimeError):
    """Another server holds the instance lock: it is running, or still starting up."""


# ===== One server per connection file, even when two start at once =====


class InstanceLock:
    """An OS lock held for the server's life, like a named Mutex in .NET. Several VS Code windows
    opening together each start the extension, and each may start a server before any of them
    has written the connection file; without this lock they would all run, the extras orphaned on
    their own ports. The OS frees the lock when the process ends, so a crash leaves nothing to
    clean up. It is named for the connection file, so a test with its own file never collides
    with the real server."""

    def __init__(self, info_path: Path) -> None:
        digest = hashlib.sha256(str(info_path.resolve()).lower().encode()).hexdigest()[:16]
        taken = AlreadyStartingError(
            "Another SwissArmyGrid server is running or starting; VS Code's status bar item shows it."
        )
        self._handle: Any = None
        if sys.platform == "win32":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.restype = ctypes.c_void_p
            kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
            # CreateMutexW leaves the last error alone when it makes a new mutex, so clear it first.
            ctypes.set_last_error(0)
            handle = kernel32.CreateMutexW(None, True, f"Local\\SwissArmyGrid-{digest}")
            if not handle or ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
                if handle:
                    kernel32.CloseHandle(handle)
                raise taken
            self._kernel32 = kernel32
            self._handle = handle
        else:
            import fcntl

            info_path.parent.mkdir(parents=True, exist_ok=True)
            handle = (info_path.parent / f".swissarmygrid-{digest}.lock").open("w")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                handle.close()
                raise taken from error
            self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        if sys.platform == "win32":
            self._kernel32.ReleaseMutex(self._handle)
            self._kernel32.CloseHandle(self._handle)
        else:
            self._handle.close()
        self._handle = None


# ===== Is a server already running? =====


def process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_info(path: Path) -> dict[str, Any] | None:
    try:
        info: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return info if isinstance(info, dict) else None


def healthy(info: dict[str, Any], timeout: float = 2.0) -> bool:
    """True when the server named in `info` answers /health with that token."""
    port, token = info.get("port"), info.get("token")
    if not isinstance(port, int) or not isinstance(token, str):
        return False
    request = urllib.request.Request(
        f"http://{_LOCALHOST}:{port}/health", headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed http://127.0.0.1 URL)
            return bool(response.status == 200)
    except (urllib.error.URLError, OSError):
        return False


def running_server(path: Path) -> dict[str, Any] | None:
    """The info of a live, healthy server, or None (no file, or a stale one)."""
    info = read_info(path)
    if info is None:
        return None
    pid = info.get("pid")
    if isinstance(pid, int) and not process_alive(pid):
        return None
    return info if healthy(info) else None


# ===== Choosing and binding the port =====


def _bind(port: int) -> socket.socket:
    # IPPROTO_TCP, not the default 0: asyncio turns Nagle off on each accepted connection only when
    # the socket says it is TCP. Without it every reply on a kept-alive connection (the page fetching
    # grid windows as it scrolls) waited ~40 ms for the client's delayed ACK. TCP_NODELAY on the
    # listener as well, which accepted sockets inherit.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    listener.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    if sys.platform == "win32":
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        # macOS and Linux keep a just-closed port in TIME_WAIT for a while; without SO_REUSEADDR the
        # remembered port can't be bound again right after a stop, so a restart silently moved to a
        # new port (and bookmarks broke). On these systems SO_REUSEADDR never lets a second socket
        # listen on a port someone is listening on: binding a live port still fails with EADDRINUSE.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((_LOCALHOST, port))
    except OSError:
        listener.close()
        raise
    listener.listen(128)
    return listener


def _port_busy(error: OSError) -> bool:
    return error.errno in (errno.EADDRINUSE, errno.EACCES) or getattr(error, "winerror", 0) in (
        10048,  # WSAEADDRINUSE
        10013,  # WSAEACCES: reserved by Windows (Hyper-V / WinNAT exclusion ranges)
    )


def open_listener(requested: int | None, last_port_file: Path) -> socket.socket:
    """A listening socket on 127.0.0.1, chosen as the module docstring describes."""
    if requested is not None:
        if requested != 0 and not 1024 <= requested <= 65535:
            raise PortError(f"Port {requested} is out of range; use 1024-65535, or 0 for any.")
        try:
            return _bind(requested)
        except OSError as error:
            if _port_busy(error):
                raise PortError(
                    f"Port {requested} is in use or reserved by Windows. Pick another with "
                    "--port, or leave --port off to choose a free one."
                ) from error
            raise
    last = _read_last_port(last_port_file)
    if last is not None:
        try:
            return _bind(last)
        except OSError as error:
            if not _port_busy(error):
                raise
    return _bind(0)


def _read_last_port(path: Path) -> int | None:
    try:
        value = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return value if 1024 <= value <= 65535 else None


def remember_port(path: Path, port: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(port), encoding="utf-8")


# ===== Clean exit =====

_keep_alive: list[Any] = []  # the Windows console handler must not be garbage collected


def on_exit(cleanup: Callable[[], None]) -> None:
    """Run `cleanup` on normal exit and when the Windows console window closes."""
    atexit.register(cleanup)
    if sys.platform == "win32":
        handler_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_ulong)
        ctrl_close, ctrl_logoff, ctrl_shutdown = 2, 5, 6

        def handler(event: int) -> bool:
            if event in (ctrl_close, ctrl_logoff, ctrl_shutdown):
                cleanup()
            return False  # let the default handling (exit) continue

        callback = handler_type(handler)
        _keep_alive.append(callback)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(callback, True)


def request_stop(path: Path, timeout: float = 5.0) -> bool:
    """Ask the running server to shut down. True when one was running."""
    info = running_server(path)
    if info is None:
        return False
    request = urllib.request.Request(
        f"http://{_LOCALHOST}:{info['port']}/shutdown",
        method="POST",
        headers={"Authorization": f"Bearer {info['token']}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout):  # noqa: S310 (fixed http://127.0.0.1 URL)
            pass
    except (urllib.error.URLError, OSError):
        pass
    return True
