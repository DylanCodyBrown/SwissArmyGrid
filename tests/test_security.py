"""The Host, Origin and token checks (plan section 6, M0 acceptance)."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from swissarmygrid.server.app import create_app
from swissarmygrid.server.security import WEBSOCKET_SUBPROTOCOL, LocalOnlyGuard

PORT = 8765
TOKEN = "test-token"
BASE = f"http://127.0.0.1:{PORT}"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
# TestClient sends "Host: testserver" on WebSockets whatever base_url says, so set it.
WS_HOST = {"Host": f"127.0.0.1:{PORT}"}


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(PORT, TOKEN), base_url=BASE)


def test_health_with_token_and_no_origin_is_ok(client: TestClient) -> None:
    assert client.get("/health", headers=AUTH).status_code == 200


def test_localhost_name_is_accepted(client: TestClient) -> None:
    headers = AUTH | {"Host": f"localhost:{PORT}", "Origin": f"http://localhost:{PORT}"}
    assert client.get("/health", headers=headers).status_code == 200


def test_missing_token_is_401(client: TestClient) -> None:
    assert client.get("/health").status_code == 401


def test_wrong_token_is_401(client: TestClient) -> None:
    assert client.get("/health", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_non_ascii_token_is_401_not_a_crash(client: TestClient) -> None:
    # Raw bytes, because the test client refuses to encode a non-ASCII str header itself.
    headers = {b"Authorization": "Bearer café".encode()}
    assert client.get("/health", headers=headers).status_code == 401


def test_foreign_origin_is_403_even_with_token(client: TestClient) -> None:
    headers = AUTH | {"Origin": "https://evil.example"}
    assert client.get("/health", headers=headers).status_code == 403


def test_own_origin_is_ok(client: TestClient) -> None:
    assert client.get("/health", headers=AUTH | {"Origin": BASE}).status_code == 200


def test_rebinding_host_is_403(client: TestClient) -> None:
    headers = AUTH | {"Host": f"evil.example:{PORT}"}
    assert client.get("/health", headers=headers).status_code == 403


def test_wrong_port_in_host_is_403(client: TestClient) -> None:
    assert client.get("/health", headers=AUTH | {"Host": "127.0.0.1:9999"}).status_code == 403


def test_grid_shell_needs_no_token_but_still_checks_origin(client: TestClient) -> None:
    assert client.get("/g/abc").status_code == 200
    assert client.get("/g/abc", headers={"Origin": "https://evil.example"}).status_code == 403


def test_data_routes_need_the_token(client: TestClient) -> None:
    assert client.get("/grids/abc").status_code == 401
    assert client.get("/grids/abc/rows").status_code == 401
    assert client.put("/datasets/x", content=b"{}").status_code == 401


@pytest.mark.parametrize(
    ("headers", "status"),
    [
        ({"Authorization": f"bearer {TOKEN}"}, 401),  # the scheme is matched exactly
        ({"Authorization": TOKEN}, 401),
        ({"Authorization": f"Bearer  {TOKEN}"}, 401),
        (AUTH | {"Host": f"LOCALHOST:{PORT}"}, 403),
        (AUTH | {"Host": f"[::1]:{PORT}"}, 403),
        (AUTH | {"Host": "127.0.0.1"}, 403),
        (AUTH | {"Host": f"localhost.:{PORT}"}, 403),
        (AUTH | {"Origin": "null"}, 403),  # a sandboxed frame or a file:// page
        (AUTH | {"Origin": f"https://127.0.0.1:{PORT}"}, 403),
        (AUTH | {"Origin": ""}, 403),
    ],
)
def test_odd_headers_are_refused(client: TestClient, headers: dict[str, str], status: int) -> None:
    assert client.get("/health", headers=headers).status_code == status


def test_a_token_in_the_query_string_is_not_accepted(client: TestClient) -> None:
    assert client.get(f"/health?token={TOKEN}").status_code == 401


def test_only_the_exact_workspace_path_skips_the_token(client: TestClient) -> None:
    assert client.get("/workspace").status_code == 200
    # A prefix match let any GET under /workspace... through the token check.
    assert client.get("/workspaceanything").status_code == 401
    assert client.get("/workspace/active").status_code == 401


# The guard on its own, around a tiny echo socket, to test the WebSocket paths.
async def _echo(websocket: WebSocket) -> None:
    await websocket.accept(subprotocol=WEBSOCKET_SUBPROTOCOL)
    await websocket.send_text("hello")
    await websocket.close()


@pytest.fixture
def ws_client() -> TestClient:
    app = Starlette(routes=[WebSocketRoute("/ws", _echo)])
    return TestClient(LocalOnlyGuard(app, PORT, TOKEN), base_url=BASE)


def test_websocket_token_as_subprotocol_is_accepted(ws_client: TestClient) -> None:
    with ws_client.websocket_connect(
        "/ws", subprotocols=[WEBSOCKET_SUBPROTOCOL, TOKEN], headers=WS_HOST | {"Origin": BASE}
    ) as websocket:
        assert websocket.receive_text() == "hello"


def test_websocket_without_token_is_refused(ws_client: TestClient) -> None:
    with (
        pytest.raises(WebSocketDisconnect),
        ws_client.websocket_connect(
            "/ws", subprotocols=[WEBSOCKET_SUBPROTOCOL], headers=WS_HOST | {"Origin": BASE}
        ),
    ):
        pass


def test_websocket_from_foreign_origin_is_refused(ws_client: TestClient) -> None:
    with (
        pytest.raises(WebSocketDisconnect),
        ws_client.websocket_connect(
            "/ws",
            subprotocols=[WEBSOCKET_SUBPROTOCOL, TOKEN],
            headers=WS_HOST | {"Origin": "https://evil.example"},
        ),
    ):
        pass
