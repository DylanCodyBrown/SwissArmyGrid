"""The Workbench window, for the agent: any command of its palette, run in the window and answered.

The golden rule (the owner, 2026-11-14): whatever the user can do, the agent can do too. The window's
own actions (show a pane, switch documents, add or delete a cell, run the statement at the cursor) live
in the page, so the server is the go-between, as it is for snapshots:

    agent  --command-->  server  --"workbench_command" to the Workbench windows-->  window
    agent  <--answer---  server  <--the window's answer (the command's result)----  window

The window's command list (workbenchCommands.ts) is the one the palette and the keys use; the command
"commands" lists it. Only the window answers (the door refuses the agent), so an agent cannot make up
the window's answer. Nothing is kept; each command is written to the audit log.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from typing import Any

from swissarmygrid.services.workspace import Conflict, Workspace

_COMMAND = re.compile(r"^[A-Za-z][A-Za-z.]{0,63}$")
# Long enough for a command that runs code and waits for it (run.cell, run.primary).
ANSWER_SECONDS: float = 600.0

Answer = asyncio.Future[Any]


class WindowRefused(Exception):
    """The window could not do it (no such command, nothing open...): its reason."""


class WindowUnanswered(Exception):
    """No Workbench window answered in time."""


class WorkbenchWindow:
    def __init__(self, ws: Workspace, answer_seconds: float = ANSWER_SECONDS) -> None:
        self.ws = ws
        self.answer_seconds = answer_seconds
        self._pending: dict[str, Answer] = {}

    async def ask(self, command: object, args: object, who: str) -> Any:
        """Have the Workbench window run `command` with `args`; its result."""
        if not isinstance(command, str) or not _COMMAND.match(command):
            raise ValueError(
                'command is a Workbench command\'s name, such as "documents" or "cell.insertBelow".'
            )
        if args is None:
            args = {}
        if not isinstance(args, dict):
            raise ValueError("args is an object of the command's arguments.")
        if not self.ws.live.workbench_open():
            raise Conflict(
                "The Workbench window is not open: open it from the grid page's Workbench tab. "
                "(Files and kernels work without it: workbench_open, workbench_run.)"
            )
        request_id = secrets.token_hex(6)
        waiting: Answer = asyncio.get_running_loop().create_future()
        self._pending[request_id] = waiting
        try:
            await self.ws.live.tell_workbench(
                {"type": "workbench_command", "id": request_id, "command": command, "args": args}
            )
            result = await asyncio.wait_for(waiting, self.answer_seconds)
        except TimeoutError:
            raise WindowUnanswered(
                f"The Workbench window did not answer within {self.answer_seconds:.0f} seconds. A run "
                'goes on in its kernel: the command "documents" shows its state.'
            ) from None
        finally:
            self._pending.pop(request_id, None)
        self.ws.audit.write(who, "workbench_window", None, command=command)
        return result

    def waiting(self, request_id: str) -> Answer | None:
        """The request still waiting for that answer, if there is one."""
        found = self._pending.get(request_id)
        return None if found is None or found.done() else found

    @staticmethod
    def answer(waiting: Answer, body: object) -> bool:
        """The window's answer: {"result": ...} or {"error": "why"}. False when the request ended
        (timed out, or another window answered first)."""
        if waiting.done():
            return False
        if isinstance(body, dict) and isinstance(body.get("error"), str):
            waiting.set_exception(WindowRefused(body["error"]))
        else:
            waiting.set_result(body.get("result") if isinstance(body, dict) else None)
        return True
