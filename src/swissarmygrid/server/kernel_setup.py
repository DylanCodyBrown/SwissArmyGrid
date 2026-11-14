"""What a Workbench kernel does once it starts (server/kernels.py runs `setup()` quietly): the package, the
connections by their names, and the corrections that make a script behave as it would on its own.

  - stdout before stderr: a write to stderr first sends what stdout holds, so a print and the error
    after it come out in that order (the kernel sends each stream on its own timer);
  - help (`?name`, `name??`, `help()` pages) shows as output, not in a pager the page has no room for;
  - `exit()`, `quit()` and `sys.exit(n)` say what happened in one line instead of a notice and a warning;
  - `%pip install` works in a Python made by uv, which has no pip: it runs `uv pip install` for this Python;
  - `asyncio.run(...)` works: the kernel's own event loop is running, so the coroutine runs to its end on
    a loop of its own in a helper thread (the cell waits for it, and Interrupt still stops the wait).

It lives with the server because it starts processes (%pip), but it runs in the kernel's Python, which
may have only the plain client (server/environments.py lends a copy of the package): standard library
only, and nothing here imports the server's own modules.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import threading
import warnings
from typing import Any


def _ipython() -> Any:
    module = sys.modules.get("IPython")
    return module.get_ipython() if module is not None else None


def _ordered_streams() -> None:
    """A write to stderr first flushes stdout."""
    err, out = sys.stderr, sys.stdout
    write = err.write

    def ordered(text: str) -> int:
        try:
            out.flush()
        except Exception:  # noqa: S110 (a closed stdout must not stop the error)
            pass
        return write(text)

    try:
        err.write = ordered  # type: ignore[method-assign]
    except (AttributeError, TypeError):
        pass


class _Exit:
    """exit() and quit(): end the script's run with a plain line, not the kernel."""

    def __repr__(self) -> str:
        return "Use exit() to end a script's run; Restart starts a fresh Python."

    def __call__(self, code: Any = None) -> None:
        raise SystemExit(code)


def _quiet_exits(shell: Any) -> None:
    """sys.exit() and exit() in a cell or a run file: one plain line, not a traceback and a warning.
    IPython catches SystemExit itself (before any custom handler), then shows it with showtraceback."""
    original = shell.showtraceback

    def showtraceback(exc_tuple: Any = None, *args: Any, **kwargs: Any) -> Any:
        kind, value, _ = exc_tuple or sys.exc_info()
        if kind is not None and issubclass(kind, SystemExit):
            code = getattr(value, "code", None)
            print(f"The code ended with sys.exit({'' if code is None else repr(code)}).")
            return None
        return original(exc_tuple, *args, **kwargs)

    shell.showtraceback = showtraceback
    warnings.filterwarnings("ignore", message="To exit: use 'exit', 'quit', or Ctrl-D.")


def _pip(line: str) -> None:
    """%pip install ...: pip when this Python has it, else uv for this Python."""
    args = line.split()
    probe = [sys.executable, "-m", "pip", "--version"]
    has_pip = subprocess.run(probe, capture_output=True, check=False).returncode == 0  # noqa: S603 (this Python)
    uv = shutil.which("uv")
    if has_pip:
        command = [sys.executable, "-m", "pip", *args]
    elif uv and args and args[0] in ("install", "uninstall", "list", "show", "freeze"):
        command = [uv, "pip", args[0], "--python", sys.executable, *args[1:]]
    else:
        print("This Python has no pip, and uv was not found: install packages from the Environment box.")
        return
    done = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603 (the user's own command)
    print(done.stdout, end="")
    if done.stderr:
        print(done.stderr, end="", file=sys.stderr)
    if done.returncode == 0 and args and args[0] == "install":
        print("Installed. Restart the kernel if a package was already imported.")


def _patched_asyncio_run() -> None:
    original = asyncio.run

    def run(main: Any, *, debug: bool | None = None, **kwargs: Any) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return original(main, debug=debug, **kwargs)
        # The kernel's loop is running in this thread: run the coroutine on its own loop elsewhere.
        result: dict[str, Any] = {}

        def work() -> None:
            try:
                result["value"] = original(main, debug=debug, **kwargs)
            except BaseException as error:  # handed back to the caller below
                result["error"] = error

        helper = threading.Thread(target=work, name="asyncio.run", daemon=True)
        helper.start()
        while helper.is_alive():
            helper.join(0.1)  # in short waits, so Interrupt (KeyboardInterrupt) still gets through
        if "error" in result:
            raise result["error"]
        return result.get("value")

    asyncio.run = run


def setup() -> None:
    shell = _ipython()
    try:
        import swissarmygrid  # noqa: F401 (the watcher, the %%sql cells, sag.*)
        from swissarmygrid.client.kernel_connections import install
        from swissarmygrid.client.table_cells import install as table_cells

        install(shell)
        table_cells(shell)  # %%sag_table: a table's read-only cell, safe to run again
    except ImportError:
        pass
    _ordered_streams()
    _patched_asyncio_run()
    # Plotly's own notebook renderer needs nbformat, which a fresh environment may lack.
    os.environ.setdefault("PLOTLY_RENDERER", "plotly_mimetype")
    if shell is None:
        return
    # Help (len?, help()) shows as an output, not a pager payload the page would never see. The hook is
    # read at the shell's start, so it is set here directly (90 runs before ipykernel's own 99).
    from IPython.core import page

    shell.set_hook("show_in_pager", page.as_hook(page.display_page), 90)  # type: ignore[no-untyped-call]
    shell.user_ns["exit"] = shell.user_ns["quit"] = _Exit()
    _quiet_exits(shell)
    shell.register_magic_function(_pip, magic_kind="line", magic_name="pip")
    sys.modules.setdefault("__sag_setup_done__", sys)  # once per kernel
