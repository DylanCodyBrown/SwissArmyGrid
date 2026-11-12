"""Snapshots: the agent asks, the user's open page draws itself to a PNG, the agent gets the picture.

The server never renders anything. It relays the agent's request to the page (a `snapshot_request`
notice on the live socket), waits for that page to upload the PNG it drew, and hands the bytes back.
The picture is the page as the user sees it, and the audit log records each one. Nothing is written to disk.
"""

from __future__ import annotations

import json
import struct
import threading
import zlib
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from tests.conftest import AGENT, AUTH, PORT, grid_id, make_client, push
from tests.test_agent import CLIENTS

PAGE = AUTH | {"Host": f"127.0.0.1:{PORT}"}


def tiny_png(width: int = 3, height: int = 2) -> bytes:
    """A real PNG (grey pixels), small enough to write inline."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    rows = b"".join(b"\x00" + b"\x80" * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def ask_in_a_thread(client: TestClient, body: dict[str, Any]) -> tuple[threading.Thread, dict[str, Any]]:
    """The agent's request blocks until the page answers, so it runs beside the page."""
    result: dict[str, Any] = {}

    def run() -> None:
        result["response"] = client.post("/snapshots", json=body, headers=AGENT)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, result


def test_the_agent_gets_the_picture_the_page_draws(client: TestClient) -> None:
    push(client, "clients", CLIENTS)
    png = tiny_png(30, 20)
    with client.websocket_connect("/ws?workspace=1", headers=PAGE) as page:
        thread, result = ask_in_a_thread(client, {"area": "grid", "scale": 2})
        request = page.receive_json()
        assert request["type"] == "snapshot_request" and request["area"] == "grid" and request["scale"] == 2
        sent = client.post(
            f"/snapshots/{request['id']}?width=30&height=20",
            content=png,
            headers=AUTH | {"Content-Type": "image/png"},
        )
        assert sent.status_code == 200
        thread.join(10)
    response = result["response"]
    assert response.status_code == 200 and response.headers["content-type"] == "image/png"
    assert response.content == png
    assert response.headers["x-snapshot-width"] == "30" and response.headers["x-snapshot-height"] == "20"


def test_it_is_audited(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        push(client, "clients", CLIENTS)
        with client.websocket_connect("/ws?workspace=1", headers=PAGE) as page:
            thread, result = ask_in_a_thread(client, {"area": "app"})
            request = page.receive_json()
            client.post(
                f"/snapshots/{request['id']}",
                content=tiny_png(),
                headers=AUTH | {"Content-Type": "image/png"},
            )
            thread.join(10)
        assert result["response"].status_code == 200
    entries = [json.loads(line) for line in (tmp_path / "state" / "audit.log").read_text().splitlines()]
    snap = next(e for e in entries if e["action"] == "snapshot")
    assert snap["actor"] == "agent" and snap["area"] == "app" and snap["bytes"] > 0


def test_with_no_page_open_the_agent_is_told_so(client: TestClient) -> None:
    response = client.post("/snapshots", json={}, headers=AGENT)
    assert response.status_code == 409 and "No SwissArmyGrid page is open" in response.text


def test_a_page_that_cannot_draw_says_why(client: TestClient) -> None:
    with client.websocket_connect("/ws?workspace=1", headers=PAGE) as page:
        thread, result = ask_in_a_thread(client, {})
        request = page.receive_json()
        failed = client.post(
            f"/snapshots/{request['id']}/error",
            content=b"the canvas is tainted",
            headers=AUTH | {"Content-Type": "text/plain"},
        )
        assert failed.status_code == 200
        thread.join(10)
    assert result["response"].status_code == 502 and "the canvas is tainted" in result["response"].text


def test_a_page_that_never_answers_times_out(tmp_path: Path) -> None:
    with make_client(tmp_path, snapshot_timeout_seconds=0.5) as client:
        with client.websocket_connect("/ws?workspace=1", headers=PAGE):
            response = client.post("/snapshots", json={}, headers=AGENT)
    assert response.status_code == 504 and "did not answer" in response.text


@pytest.mark.parametrize(
    "body",
    [{"area": "kitchen"}, {"scale": 3}, {"scale": 0}, {"scale": True}, ["not", "an", "object"]],
)
def test_a_bad_request_is_refused_before_the_page_is_asked(client: TestClient, body: Any) -> None:
    with client.websocket_connect("/ws?workspace=1", headers=PAGE):
        assert client.post("/snapshots", json=body, headers=AGENT).status_code == 400


def test_only_the_page_can_upload_and_only_a_png(client: TestClient) -> None:
    push(client, "clients", CLIENTS)
    with client.websocket_connect("/ws?workspace=1", headers=PAGE) as page:
        thread, result = ask_in_a_thread(client, {})
        request = page.receive_json()
        url = f"/snapshots/{request['id']}"
        # The agent cannot answer its own request with a picture of its choosing.
        assert (
            client.post(url, content=tiny_png(), headers=AGENT | {"Content-Type": "image/png"}).status_code
            == 403
        )
        assert client.post(url, content=b"not a picture", headers=AUTH).status_code == 400
        assert client.post("/snapshots/nope", content=tiny_png(), headers=AUTH).status_code == 404
        client.post(url, content=tiny_png(), headers=AUTH | {"Content-Type": "image/png"})
        thread.join(10)
    assert result["response"].status_code == 200
    # An answer after the request is over finds nothing waiting.
    assert client.post(url, content=tiny_png(), headers=AUTH).status_code == 404


def test_an_oversized_picture_is_refused(tmp_path: Path) -> None:
    with make_client(tmp_path, max_snapshot_bytes=20) as client:
        with client.websocket_connect("/ws?workspace=1", headers=PAGE) as page:
            thread, result = ask_in_a_thread(client, {})
            request = page.receive_json()
            too_big = client.post(f"/snapshots/{request['id']}", content=tiny_png(40, 40), headers=AUTH)
            assert too_big.status_code == 413
            client.post(f"/snapshots/{request['id']}/error", content=b"too big", headers=AUTH)
            thread.join(10)
    assert result["response"].status_code == 502


def test_an_answer_that_arrives_as_the_request_times_out_is_not_a_500(tmp_path: Path) -> None:
    """The upload checked that a request was waiting, then read the body; if the request timed out
    meanwhile, setting its result raised InvalidStateError: a 500. Needs a real socket, to send the
    body only after the timeout."""
    import socket
    import time

    import uvicorn
    from websockets.sync.client import connect

    from swissarmygrid.server.app import create_app
    from swissarmygrid.settings import Settings

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    settings = Settings(
        state_dir=tmp_path / "state", export_dir=tmp_path / "exports", snapshot_timeout_seconds=0.5
    )
    server = uvicorn.Server(
        uvicorn.Config(create_app(port, "t", settings, audit_log=False), port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    try:
        with connect(
            f"ws://127.0.0.1:{port}/ws?workspace=1",
            subprotocols=["swissarmygrid", "t"],  # type: ignore[list-item]
        ) as page:
            asked: dict[str, Any] = {}

            def ask() -> None:
                import urllib.error
                import urllib.request

                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/snapshots",
                    data=b"{}",
                    method="POST",
                    headers={"Authorization": "Bearer t"},
                )
                try:
                    urllib.request.urlopen(request, timeout=10)  # noqa: S310
                except urllib.error.HTTPError as error:
                    asked["status"] = error.code

            agent = threading.Thread(target=ask)
            agent.start()
            snapshot_id = json.loads(page.recv(timeout=10))["id"]
            png = tiny_png()
            upload = socket.create_connection(("127.0.0.1", port))
            upload.sendall(
                f"POST /snapshots/{snapshot_id} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                f"Authorization: Bearer t\r\nContent-Length: {len(png)}\r\nConnection: close\r\n\r\n".encode()
            )
            agent.join(10)  # the request times out while the upload's body is still on its way
            assert asked.get("status") == 504
            upload.sendall(png)
            reply = b""
            while chunk := upload.recv(4096):
                reply += chunk
            upload.close()
        status = int(reply.split(b" ", 2)[1])
        assert status in (404, 409), reply[:200]
    finally:
        server.should_exit = True
        thread.join(10)


def test_an_oversized_upload_is_refused_before_it_is_read_whole(tmp_path: Path) -> None:
    with make_client(tmp_path, max_snapshot_bytes=20) as client:
        with client.websocket_connect("/ws?workspace=1", headers=PAGE) as page:
            thread, result = ask_in_a_thread(client, {})
            request = page.receive_json()
            headers = AUTH | {"Content-Length": str(10**9)}
            too_big = client.post(f"/snapshots/{request['id']}", content=tiny_png(40, 40), headers=headers)
            assert too_big.status_code == 413
            client.post(f"/snapshots/{request['id']}/error", content=b"x" * 10_000, headers=AUTH)
            thread.join(10)
    assert result["response"].status_code == 502 and len(result["response"].text) < 500


def test_a_grid_page_also_counts_as_an_open_page(client: TestClient) -> None:
    gid = grid_id(push(client, "clients", CLIENTS))
    with client.websocket_connect(f"/ws?grid={gid}", headers=PAGE) as page:
        thread, result = ask_in_a_thread(client, {})
        request = page.receive_json()
        assert request["type"] == "snapshot_request" and request["area"] == "app"
        client.post(f"/snapshots/{request['id']}", content=tiny_png(), headers=AUTH)
        thread.join(10)
    assert result["response"].status_code == 200
