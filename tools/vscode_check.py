"""Open a SwissArmyGrid grid in VS Code's built-in browser and screenshot it.

Starts a throwaway VS Code window (its own empty profile, remote debugging on port 9333),
opens "Simple Browser: Show" through the command palette with the given URL, waits, saves a
screenshot, then closes that window. Your normal VS Code windows and settings are untouched.
The window is driven over the Chrome DevTools Protocol, the same protocol Chrome's devtools use.

    uv run python tools/vscode_check.py "<grid url with #token=...>" shot.png
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from typing import Any

import websockets

PORT = 9333


def targets() -> list[dict[str, Any]]:
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=5) as response:
        found: list[dict[str, Any]] = json.loads(response.read())
        return found


def code_exe() -> str:
    found = shutil.which("code")
    if found and found.lower().endswith(("code", "code.cmd")):
        return os.path.join(os.path.dirname(os.path.dirname(found)), "Code.exe")
    return found or r"C:\Program Files\Microsoft VS Code\Code.exe"


class Cdp:
    def __init__(self, socket: Any) -> None:
        self.socket = socket
        self.counter = 0

    async def call(self, method: str, **params: Any) -> dict[str, Any]:
        self.counter += 1
        mine = self.counter
        await self.socket.send(json.dumps({"id": mine, "method": method, "params": params}))
        while True:
            message: dict[str, Any] = json.loads(await self.socket.recv())
            if message.get("id") == mine:
                result: dict[str, Any] = message.get("result", message.get("error", {}))
                return result

    async def key(self, key: str, code: str, virtual_key: int) -> None:
        for kind in ("rawKeyDown", "keyUp"):
            await self.call(
                "Input.dispatchKeyEvent", type=kind, key=key, code=code, windowsVirtualKeyCode=virtual_key
            )


async def drive(url: str, out: str) -> None:
    for _ in range(60):
        try:
            pages = [t for t in targets() if t["type"] == "page" and "workbench" in t.get("url", "")]
            if pages:
                break
        except OSError:
            pass
        time.sleep(1)
    else:
        sys.exit("VS Code did not open its workbench.")
    async with websockets.connect(pages[0]["webSocketDebuggerUrl"], max_size=None) as socket:
        cdp = Cdp(socket)
        await asyncio.sleep(4)  # let the workbench settle
        await cdp.key("F1", "F1", 112)
        await asyncio.sleep(1)
        await cdp.call("Input.insertText", text="Simple Browser: Show")
        await asyncio.sleep(1.5)
        await cdp.key("Enter", "Enter", 13)
        await asyncio.sleep(1.5)
        await cdp.call("Input.insertText", text=url)
        await asyncio.sleep(0.5)
        await cdp.key("Enter", "Enter", 13)
        await asyncio.sleep(6)
        shot = await cdp.call("Page.captureScreenshot", format="png")
        with open(out, "wb") as handle:
            handle.write(base64.b64decode(shot["data"]))
    print("grid pages:", [t["url"][:80] for t in targets() if "127.0.0.1" in t.get("url", "")])


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    url, out = sys.argv[1], sys.argv[2]
    profile = tempfile.mkdtemp(prefix="sag_vscode_")
    subprocess.Popen(
        [
            code_exe(),
            "--user-data-dir",
            os.path.join(profile, "data"),
            "--extensions-dir",
            os.path.join(profile, "ext"),
            f"--remote-debugging-port={PORT}",
            "--new-window",
            "--disable-workspace-trust",
            "--skip-welcome",
            "--skip-release-notes",
        ]
    )
    try:
        asyncio.run(drive(url, out))
    finally:
        # Close only the window this script started (its profile folder name is unique).
        name = os.path.basename(profile)
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='Code.exe'\" | "
                f"Where-Object {{ $_.CommandLine -like '*{name}*' }} | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
            ],
            check=False,
        )


if __name__ == "__main__":
    main()
