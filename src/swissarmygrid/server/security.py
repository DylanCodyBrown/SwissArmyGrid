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

from starlette.datastructures import Headers
from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocket

WEBSOCKET_SUBPROTOCOL: str = "swissarmygrid"

# The page shell and its files: prefixes for /g/<id> and /static/..., and the exact paths of the
# workspace, catalog and notebook pages (a prefix would also let "/workspaceanything" skip the token
# check). /marimo/... is the notebook's frame: it needs the notebook cookie or the token.
_PUBLIC_PREFIXES: tuple[str, ...] = ("/g/", "/static/")
_PUBLIC_PATHS: frozenset[str] = frozenset({"/workspace", "/catalog", "/notebook"})

_MESSAGES: dict[int, str] = {
    401: "Missing or wrong token. Clients read it from the server's connection file.",
    403: "Refused: only this computer's own SwissArmyGrid pages and clients may call it.",
}


class LocalOnlyGuard:
    """ASGI middleware enforcing the Host, Origin and token checks."""

    def __init__(self, app: ASGIApp, port: int, token: str, notebook_cookie: str = "") -> None:
        self._app = app
        self._token: bytes = token.encode("ascii")
        # The notebook's frame (marimo, served at /marimo) cannot add the token to its requests; the
        # notebook page, which has the token, gets this cookie for it (routes_notebook.py).
        self._notebook_cookie: bytes = notebook_cookie.encode("ascii")
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
        if path == "/marimo" or path.startswith("/marimo/"):
            return None if self._has_notebook_cookie(headers) or self._has_token(scope, headers) else 401
        public = path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES)
        if scope["type"] == "http" and scope["method"] == "GET" and public:
            return None
        return None if self._has_token(scope, headers) else 401

    def _has_notebook_cookie(self, headers: Headers) -> bool:
        if not self._notebook_cookie:
            return False
        for part in headers.get("cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "sag_notebook" and secrets.compare_digest(
                value.encode("latin-1"), self._notebook_cookie
            ):
                return True
        return False

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
