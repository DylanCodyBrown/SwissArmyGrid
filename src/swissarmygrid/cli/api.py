"""The CLI's one door to the grid server: HTTP calls as the agent, and the live event socket.

Everything the agent does goes through `call`, which finds the running server through the
connection file and sends `X-Actor: agent`. The server keeps every rule (approvals, the SQL guard,
the audit log); this module only carries requests. Think of it as the typed
`HttpClient` a console client would use.

It speaks HTTP/1.1 over a plain socket instead of `urllib` or `http.client`: those pull in the email
parser, `ssl` and `inspect` (about 100 ms of imports on a laptop), and a loopback request needs none
of it. There is no separate health check either: a refused connection already says "not running".

Standard library only, and nothing from the rest of the package, so `sag` starts fast.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

NOT_RUNNING = (
    "SwissArmyGrid isn't running. It runs inside VS Code: open VS Code and the SwissArmyGrid "
    "extension starts it (or click SwissArmyGrid in VS Code's status bar to start it again)."
)


class CommandError(Exception):
    """A command failed. The message is the plain-language reason, safe to show the agent."""


def info_file() -> Path:
    """The server's connection file; SWISSARMYGRID_INFO overrides it (tests use this)."""
    override = os.environ.get("SWISSARMYGRID_INFO")
    return Path(override) if override else Path.home() / ".swissarmygrid" / "server.json"


def _info() -> dict[str, Any]:
    try:
        info: Any = json.loads(info_file().read_text(encoding="utf-8"))
        return {"port": int(info["port"]), "token": str(info["token"])}
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise CommandError(NOT_RUNNING) from error


def _query_value(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def _dechunk(payload: bytes) -> bytes:
    """Join an HTTP chunked body (hex length, CRLF, data, CRLF... ending in a zero chunk)."""
    out = bytearray()
    while payload:
        size_line, _, payload = payload.partition(b"\r\n")
        size = int(size_line.split(b";")[0] or b"0", 16)
        if size == 0:
            break
        out += payload[:size]
        payload = payload[size + 2 :]
    return bytes(out)


def _http(
    info: dict[str, Any],
    method: str,
    target: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
) -> tuple[int, dict[str, str], bytes]:
    """One request, one response, then the connection closes: status, lower-cased headers, body."""
    lines = [f"{method} {target} HTTP/1.1", f"Host: 127.0.0.1:{info['port']}", "Connection: close"]
    lines += [f"{k}: {v}" for k, v in headers.items()]
    if body is not None:
        lines.append(f"Content-Length: {len(body)}")
    request = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + (body or b"")
    try:
        with socket.create_connection(("127.0.0.1", info["port"]), timeout=timeout) as sock:
            sock.sendall(request)
            chunks: list[bytes] = []
            while chunk := sock.recv(65536):
                chunks.append(chunk)
    except TimeoutError as error:
        raise CommandError(f"The server did not answer within {timeout:.0f} seconds.") from error
    except OSError as error:  # refused, reset: no server behind the connection file
        raise CommandError(NOT_RUNNING) from error
    head, _, payload = b"".join(chunks).partition(b"\r\n\r\n")
    status_line, *header_lines = head.decode("latin-1").split("\r\n")
    try:
        status = int(status_line.split()[1])
    except (IndexError, ValueError) as error:
        raise CommandError("The server sent a reply this client can't read.") from error
    fields = {k.strip().lower(): v.strip() for k, _, v in (line.partition(":") for line in header_lines)}
    if fields.get("transfer-encoding", "").lower() == "chunked":
        payload = _dechunk(payload)
    return status, fields, payload


def call(
    method: str,
    path: str,
    body: Any = None,
    params: dict[str, Any] | None = None,
    timeout: float = 240.0,
) -> Any:
    """One request to the grid server, as the agent. Returns the parsed JSON (None when empty).
    A string body goes as plain text (the Paste button's route reads raw text); anything else as JSON."""
    info = _info()
    clean = {k: _query_value(v) for k, v in (params or {}).items() if v is not None}
    target = path + (("?" + urlencode(clean)) if clean else "")
    data = None if body is None else body.encode() if isinstance(body, str) else json.dumps(body).encode()
    headers = {
        "Authorization": f"Bearer {info['token']}",
        "Content-Type": "text/plain" if isinstance(body, str) else "application/json",
        "X-Actor": "agent",
    }
    status, _, payload = _http(info, method, target, headers, data, timeout)
    # A 3xx is not an answer either: the server redirects a path with a trailing slash (an empty id).
    if status >= 300:
        raise CommandError(f"{status}: {payload.decode('utf-8', 'replace') or 'no such address'}")
    try:
        return json.loads(payload or b"null")
    except ValueError as error:
        raise CommandError("The server sent a reply this client can't read.") from error


def call_lines(method: str, path: str, body: Any = None, timeout: float = 900.0) -> list[Any]:
    """A request whose answer streams one JSON object a line (a Workbench run, a new environment):
    every line, in order, once the stream ends."""
    payload, _ = fetch_bytes(method, path, body, timeout)
    out = []
    for line in payload.decode("utf-8", "replace").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError as error:
                raise CommandError("The server sent a line this client can't read.") from error
    return out


def fetch_bytes(
    method: str, path: str, body: Any = None, timeout: float = 240.0
) -> tuple[bytes, dict[str, str]]:
    """Like `call` for a reply that is a file (a PNG), not JSON: the bytes and the reply's headers."""
    info = _info()
    data = None if body is None else json.dumps(body).encode()
    headers = {
        "Authorization": f"Bearer {info['token']}",
        "Content-Type": "application/json",
        "X-Actor": "agent",
    }
    status, fields, payload = _http(info, method, path, headers, data, timeout)
    if status >= 300:
        raise CommandError(f"{status}: {payload.decode('utf-8', 'replace')}")
    return payload, fields


# ===== the live event socket =====
#
# The grid page and this client hear the same broadcasts (`/ws`): a grid changed (and by whom), a
# table opened or closed, a card added, an approval asked for. A WebSocket is an HTTP upgrade
# followed by length-prefixed frames, small enough to speak by hand, which keeps this stdlib-only.


def _masked(opcode: int, payload: bytes) -> bytes:
    """A client frame (the protocol requires clients to mask what they send)."""
    key = os.urandom(4)
    length = len(payload)
    head = bytes([0x80 | opcode])
    if length < 126:
        head += bytes([0x80 | length])
    else:
        head += bytes([0x80 | 126]) + struct.pack(">H", length)
    return head + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


def read_frame(buffer: bytearray) -> tuple[int, bytes] | None:
    """Take one complete server frame off the front of `buffer`: (opcode, payload), or None
    when more bytes are needed. Server frames are never masked."""
    if len(buffer) < 2:
        return None
    opcode = buffer[0] & 0x0F
    length = buffer[1] & 0x7F
    offset = 2
    if length == 126:
        if len(buffer) < 4:
            return None
        length = struct.unpack(">H", bytes(buffer[2:4]))[0]
        offset = 4
    elif length == 127:
        if len(buffer) < 10:
            return None
        length = struct.unpack(">Q", bytes(buffer[2:10]))[0]
        offset = 10
    if len(buffer) < offset + length:
        return None
    payload = bytes(buffer[offset : offset + length])
    del buffer[: offset + length]
    return opcode, payload


def collect_events(
    types: set[str] | None, seconds: float, limit: int, path: str = "/ws?agent=1"
) -> list[dict[str, Any]]:
    """Listen to the server's broadcasts until `limit` matching events arrive or `seconds` pass.
    `types` limits which event types count (None: all). `path` is the socket address; tests connect as a
    page (`/ws?workspace=1`) to stand in for the browser. Returns the events in order."""
    info = _info()
    deadline = time.monotonic() + seconds
    found: list[dict[str, Any]] = []
    try:
        sock = socket.create_connection(("127.0.0.1", info["port"]), timeout=5)
    except OSError as error:
        raise CommandError(NOT_RUNNING) from error
    with sock:
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(
            (
                f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{info['port']}\r\n"
                f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                f"Sec-WebSocket-Version: 13\r\n"
                f"Sec-WebSocket-Protocol: swissarmygrid, {info['token']}\r\n\r\n"
            ).encode("latin-1")
        )
        buffer = bytearray()
        while b"\r\n\r\n" not in buffer:
            chunk = sock.recv(4096)
            if not chunk:
                raise CommandError("The server closed the event connection.")
            buffer += chunk
        head, _, rest = bytes(buffer).partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise CommandError("The server refused the event connection.")
        buffer = bytearray(rest)
        while len(found) < limit:
            frame = read_frame(buffer)
            if frame is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(remaining)
                try:
                    chunk = sock.recv(65536)
                except TimeoutError:
                    break
                if not chunk:
                    break
                buffer += chunk
                continue
            opcode, payload = frame
            if opcode == 9:  # ping: answer, or the server drops us
                sock.sendall(_masked(10, payload))
            elif opcode == 8:
                break
            elif opcode == 1:
                event = json.loads(payload.decode("utf-8", "replace"))
                if isinstance(event, dict) and (types is None or event.get("type") in types):
                    found.append(event)
        try:
            sock.sendall(_masked(8, b""))
        except OSError:
            pass  # best effort: the socket closes either way
    return found
