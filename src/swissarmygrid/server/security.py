"""The checks every request goes through before routing (plan section 6, rules 1 and 2).

Think of LocalOnlyGuard as ASP.NET middleware registered ahead of routing: it sees the raw
request and either short-circuits with a status code or passes it on. Two checks, in order:

1. Host and Origin -> 403. A web page open in the user's browser can try to call
   http://127.0.0.1:<port>. Checking Host defeats DNS rebinding (an attacker's domain that
   resolves to 127.0.0.1 still sends its own name as Host). Checking Origin stops a page on
   another site from calling us. A request with NO Origin header is allowed: browsers send
   Origin on cross-site requests and on every WebSocket upgrade, while non-browser clients
   such as the push client never send it. Those still face the token check.
2. Token -> 401. `Authorization: Bearer <token>`, compared in constant time. A browser cannot
   set headers on a WebSocket, so the grid page sends the token as a WebSocket subprotocol
   instead (`new WebSocket(url, ["swissarmygrid", token])`); that keeps it out of the URL and
   so out of the server's access log.

Deliberate exception: GET requests for the grid page shell (/g/...) and its static files
(/static/...) skip the token check. They hold no data, only the page and the vendored grid
library. The token reaches the page in the URL fragment (#token=...), which browsers never
send to the server, and the page's script attaches it to every data request. Host and Origin
are still checked on these paths.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocket

WEBSOCKET_SUBPROTOCOL: str = "swissarmygrid"

# The page shell and its files: prefixes for /g/<id> and /static/..., and the one exact path of
# the workspace page (a prefix would also let "/workspaceanything" skip the token check).
_PUBLIC_PREFIXES: tuple[str, ...] = ("/g/", "/static/")
_PUBLIC_PATHS: frozenset[str] = frozenset({"/workspace"})

_MESSAGES: dict[int, str] = {
    401: "Missing or wrong token. Clients read it from the server's connection file.",
    403: "Refused: only this computer's own SwissArmyGrid pages and clients may call it.",
}


class LocalOnlyGuard:
    """ASGI middleware enforcing the Host, Origin and token checks."""

    def __init__(self, app: ASGIApp, port: int, token: str) -> None:
        self._app = app
        self._token: bytes = token.encode("ascii")
        self._allowed_hosts: frozenset[str] = frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})
        self._allowed_origins: frozenset[str] = frozenset(f"http://{host}" for host in self._allowed_hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Lifespan events (startup and shutdown) are not requests.
        if scope["type"] not in ("http", "websocket"):
            await self._app(scope, receive, send)
            return

        status: int | None = self._check(scope)
        if status is None:
            await self._app(scope, receive, send)
        elif scope["type"] == "websocket":
            # Closing before accepting makes the server answer the upgrade with HTTP 403.
            await WebSocket(scope, receive, send).close(code=1008)
        else:
            await PlainTextResponse(_MESSAGES[status], status_code=status)(scope, receive, send)

    def _check(self, scope: Scope) -> int | None:
        """Return the refusal status code, or None when the request may proceed."""
        headers: Headers = Headers(scope=scope)
        if headers.get("host") not in self._allowed_hosts:
            return 403
        origin: str | None = headers.get("origin")
        if origin is not None and origin not in self._allowed_origins:
            return 403
        path: str = scope["path"]
        public = path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES)
        if scope["type"] == "http" and scope["method"] == "GET" and public:
            return None
        return None if self._has_token(scope, headers) else 401

    def _has_token(self, scope: Scope, headers: Headers) -> bool:
        # Header values arrive decoded as latin-1, so re-encoding them is lossless.
        presented: bytes = headers.get("authorization", "").encode("latin-1")
        if secrets.compare_digest(presented, b"Bearer " + self._token):
            return True
        if scope["type"] == "websocket":
            offered: list[str] = [
                part.strip() for part in headers.get("sec-websocket-protocol", "").split(",")
            ]
            return any(secrets.compare_digest(part.encode("latin-1"), self._token) for part in offered)
        return False


# ===== Engine errors and the agent's privacy level =====

# How engine errors start, wherever a route words them (context.error_response, the SQL box, the
# dataset store). DuckDB's own text can quote a cell ("Could not convert string 'Acme' to INT32").
_ENGINE_ERROR_PREFIXES: tuple[bytes, ...] = (b"DuckDB:", b"DuckDB could not store")
ENGINE_ERROR_HIDDEN: str = (
    "DuckDB could not compute this. The message is hidden at this privacy level because it can "
    "quote cell values; check the query or the grid's steps."
)
# Below this level an agent sees no engine error text (from level 2 it sees rows anyway).
ENGINE_ERRORS_SHOWN_FROM_LEVEL: int = 2


def is_agent_scope(scope: Scope) -> bool:
    return Headers(scope=scope).get("x-actor") == "agent"


class EngineErrorRedaction:
    """ASGI middleware: an agent below privacy level 2 never reads DuckDB's error text.

    Each route turns a DuckDB error into a 400 whose text is DuckDB's message, which can quote the
    value that failed (a cast, a date, a division). An agent at level 0 or 1 could otherwise read
    any cell one error at a time (`CAST(client AS INTEGER)`). Doing it here, on the response,
    covers every route, including ones added later, without each having to remember. The user and
    an agent at level 2 or 3 get the full message, which they need to fix a query, except at level 2
    with a column hidden or hashed, where the quoted cell may be that column's (`shown` decides:
    routes_agent.Policy.engine_errors_shown).
    """

    def __init__(self, app: ASGIApp, shown: Callable[[], bool]) -> None:
        self._app = app
        self._shown = shown

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not is_agent_scope(scope) or self._shown():
            await self._app(scope, receive, send)
            return
        held: Message | None = None  # an error's start message, until its body says what it is
        body = bytearray()

        async def guarded_send(message: Message) -> None:
            nonlocal held
            if message["type"] == "http.response.start" and 400 <= int(message["status"]) < 600:
                held = message
                return
            if held is None:
                await send(message)
                return
            body.extend(message.get("body", b""))
            if message.get("more_body", False):
                return
            start, text = held, bytes(body)
            if text.startswith(_ENGINE_ERROR_PREFIXES):
                text = ENGINE_ERROR_HIDDEN.encode()
                headers = [(k, v) for k, v in start.get("headers", []) if k.lower() != b"content-length"]
                start = {**start, "headers": [*headers, (b"content-length", str(len(text)).encode())]}
            held = None
            await send(start)
            await send({"type": "http.response.body", "body": text, "more_body": False})

        await self._app(scope, receive, guarded_send)
