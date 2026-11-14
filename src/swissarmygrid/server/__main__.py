"""The `swissarmygrid` command: start, stop or check the local server.

    uv run swissarmygrid                 start (reuses the last port when it is free)
    uv run swissarmygrid --port 8790     start on exactly this port, or fail clearly
    uv run swissarmygrid status          is one running, and where
    uv run swissarmygrid stop            ask the running one to shut down

SwissArmyGrid lives in VS Code: its extension runs this command when VS Code opens (with
SWISSARMYGRID_STARTED_BY=vscode), and a server it started stops once no VS Code window has been
connected for VSCODE_GONE_SECONDS. Starting it by hand is for working on SwissArmyGrid itself.

Start-up order matters (server/lifecycle.py): take the instance lock, refuse if a healthy server
is already running, bind and listen, write the connection file, serve, delete the file on every
way out. A fresh token is made on every start, so grid tabs from an earlier run must be reopened
from a push.
"""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import sys
import threading
import time
from collections.abc import Callable
from types import FrameType

import uvicorn

from swissarmygrid import __version__
from swissarmygrid.adapters.live_updates import LiveUpdates
from swissarmygrid.adapters.state_files import info_file_path, remove_info_file, write_info_file
from swissarmygrid.server.app import create_app
from swissarmygrid.server.lifecycle import (
    AlreadyRunningError,
    AlreadyStartingError,
    InstanceLock,
    PortError,
    on_exit,
    open_listener,
    remember_port,
    request_stop,
    running_server,
)
from swissarmygrid.server.routes_processes import QuietHeartbeats
from swissarmygrid.settings import Settings

# Long enough to ride out a window reload (the extension reconnects within a few seconds).
VSCODE_GONE_SECONDS: float = 30.0


class _Server(uvicorn.Server):
    """uvicorn's server, but a stop by signal (Ctrl+C, the system shutting down) first lets the waiting
    listeners answer, as `swissarmygrid stop` does."""

    def __init__(self, config: uvicorn.Config, on_exit: Callable[[], None]) -> None:
        super().__init__(config)
        self._on_exit = on_exit

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        try:
            self._on_exit()
        except Exception:  # noqa: S110 (never in the way of stopping)
            pass
        super().handle_exit(sig, frame)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the SwissArmyGrid server on localhost.")
    parser.add_argument("command", nargs="?", default="start", choices=["start", "stop", "status"])
    parser.add_argument("--port", type=int, default=None, help="Exact port; 0 picks any free one.")
    args = parser.parse_args()
    path = info_file_path()

    if args.command == "status":
        info = running_server(path)
        if info is None:
            print("SwissArmyGrid is not running.")
            sys.exit(1)
        print(f"Running on http://127.0.0.1:{info['port']} (process {info.get('pid')}).")
        return
    if args.command == "stop":
        if not request_stop(path):
            print("SwissArmyGrid is not running.")
            return
        for _ in range(50):
            if running_server(path) is None:
                print("Stopped.")
                return
            time.sleep(0.1)
        print("Asked the server to stop; it has not exited yet.")
        sys.exit(1)

    try:
        start(args.port)
    except (AlreadyRunningError, AlreadyStartingError, PortError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)


def start(port: int | None) -> None:
    settings = Settings()
    path = info_file_path()
    try:
        lock = InstanceLock(path)  # held until this process ends
    except AlreadyStartingError:
        running = running_server(path)
        if running is not None:
            raise AlreadyRunningError(running) from None  # say where, when it's up already
        raise
    existing = running_server(path)  # a server from before the lock existed (an older version)
    if existing is not None:
        raise AlreadyRunningError(existing)
    started_by = os.environ.get("SWISSARMYGRID_STARTED_BY", "cli")

    listener = open_listener(port, settings.state_dir / "last_port")
    actual_port: int = listener.getsockname()[1]
    token: str = secrets.token_urlsafe(32)

    server: uvicorn.Server | None = None

    def release() -> None:
        # Every waiting listener (each Workbench kernel and Python session has one) answers now, so
        # the stop never waits on them.
        app.state.ctx.sessions.release_waiters()

    def stop_soon() -> None:
        release()
        if server is not None:
            server.should_exit = True

    app = create_app(actual_port, token, settings, on_shutdown=stop_soon)
    config = uvicorn.Config(app, log_level="info", timeout_graceful_shutdown=3)
    # uvicorn has set up its loggers by now; keep the sessions' heartbeats out of the access log.
    logging.getLogger("uvicorn.access").addFilter(QuietHeartbeats())
    server = _Server(config, release)

    on_exit(lambda: remove_info_file(path, token))
    remember_port(settings.state_dir / "last_port", actual_port)
    write_info_file(
        path,
        {
            "port": actual_port,
            "token": token,
            "max_dataset_bytes": settings.max_dataset_bytes,
            "pid": os.getpid(),
            "version": __version__,
            "started": time.time(),
            # Who started it, and from which install: VS Code's status bar item tells its own
            # server from one started by hand or from another install (another version).
            "started_by": started_by,
            "executable": sys.executable,
        },
    )
    print(f"SwissArmyGrid {__version__} on http://127.0.0.1:{actual_port}  (connection file: {path})")
    if started_by == "vscode":
        live: LiveUpdates = app.state.ctx.live
        threading.Thread(
            target=end_with_vscode, args=(live, stop_soon), name="sag-vscode", daemon=True
        ).start()
    try:
        server.run(sockets=[listener])
    finally:
        remove_info_file(path, token)
        lock.release()


def end_with_vscode(live: LiveUpdates, stop: Callable[[], None]) -> None:
    """A server VS Code started ends with VS Code: once no window has been connected for
    VSCODE_GONE_SECONDS (from the start, too, should the extension never connect)."""
    while live.openers_gone_for() < VSCODE_GONE_SECONDS:
        time.sleep(2)
    print(f"No VS Code window for {VSCODE_GONE_SECONDS:.0f} seconds; stopping.")
    stop()


if __name__ == "__main__":
    main()
