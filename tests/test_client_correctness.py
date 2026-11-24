"""Client correctness: status assertions, WebSocket connect retry, draining, URL building."""

import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator

import pytest
from fastapi import WebSocket
from websockets.exceptions import InvalidStatus

from fastapi_testing import AsyncTestClient, Config, PortGenerator, create_test_server, get_config, set_config


@pytest.fixture
def config() -> Iterator[None]:
    """Restore the active config after a test changes it."""
    original = get_config()
    try:
        yield
    finally:
        set_config(original)


@pytest.mark.asyncio
async def test_expect_status_failure_names_both_codes_and_the_body():
    async with create_test_server() as server:

        @server.app.get("/missing")
        async def missing():
            return {"detail": "nothing here"}

        response = await server.client.get("/missing")
        with pytest.raises(AssertionError, match=r"Expected status 404, got 200: .*nothing here"):
            await response.expect_status(404)


def test_expect_status_still_fails_under_python_optimize():
    """`assert` statements vanish under `python -O`; expect_status must not."""
    script = textwrap.dedent(
        """
        import asyncio
        from fastapi_testing import create_test_server

        async def main():
            async with create_test_server() as server:
                @server.app.get("/")
                async def root():
                    return {}
                response = await server.client.get("/")
                try:
                    await response.expect_status(500)
                except AssertionError:
                    print("raised")
                else:
                    print("passed silently")

        asyncio.run(main())
        """
    )
    result = subprocess.run([sys.executable, "-O", "-c", script], capture_output=True, text=True, timeout=60)
    assert result.stdout.strip() == "raised", result.stderr


@pytest.mark.asyncio
async def test_rejected_handshake_is_not_retried(config: None):
    set_config(Config(ws_retry_attempts=3, ws_retry_delay=1.0))
    async with create_test_server() as server:
        began = time.monotonic()
        with pytest.raises(InvalidStatus):
            await server.client.websocket("/no-such-websocket")
        # Retrying would take 2 x ws_retry_delay.
        assert time.monotonic() - began < 0.9


@pytest.mark.asyncio
async def test_refused_connection_is_retried(config: None):
    set_config(Config(ws_retry_attempts=3, ws_retry_delay=0.2))
    sock = PortGenerator().bind("127.0.0.1")
    port = sock.getsockname()[1]
    sock.close()  # nothing listens on the port now
    async with AsyncTestClient(f"http://127.0.0.1:{port}") as client:
        began = time.monotonic()
        with pytest.raises(OSError):
            await client.websocket("/ws")
        # Three attempts with two delays between them.
        assert time.monotonic() - began >= 0.4


@pytest.mark.asyncio
async def test_drain_messages_returns_what_arrived_before_the_server_closed():
    async with create_test_server() as server:

        @server.app.websocket("/ws")
        async def ws(websocket: WebSocket):
            await websocket.accept()
            await websocket.send_text("a")
            await websocket.send_text("b")
            await websocket.close()

        response = await server.client.websocket("/ws")
        assert await server.client.ws.drain_messages(response, timeout=1.0) == ["a", "b"]


@pytest.mark.asyncio
async def test_drain_messages_rejects_no_timeout():
    async with create_test_server() as server:

        @server.app.websocket("/ws")
        async def ws(websocket: WebSocket):
            await websocket.accept()
            await websocket.receive_text()

        response = await server.client.websocket("/ws")
        with pytest.raises(ValueError, match="needs a timeout"):
            await server.client.ws.drain_messages(response, timeout=None)  # pyright: ignore[reportArgumentType]  # ty: ignore[invalid-argument-type]


@pytest.mark.asyncio
async def test_expect_message_timeout_says_what_it_waited_for():
    async with create_test_server() as server:

        @server.app.websocket("/ws")
        async def ws(websocket: WebSocket):
            await websocket.accept()
            await websocket.receive_text()

        response = await server.client.websocket("/ws")
        with pytest.raises(TimeoutError, match=r"No WebSocket message received within 0.1 s"):
            await server.client.ws.expect_message(response, "anything", timeout=0.1)


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("http://127.0.0.1:8001", "ws://127.0.0.1:8001/ws"),
        ("https://example.test", "wss://example.test/ws"),
        # Only the scheme is replaced, not every "http://" in the URL.
        ("http://proxy.test/http://inner", "ws://proxy.test/http://inner/ws"),
    ],
)
def test_websocket_url_maps_only_the_scheme(base_url: str, expected: str):
    assert AsyncTestClient(base_url)._websocket_url("/ws") == expected


@pytest.mark.parametrize("base_url", ["ftp://example.test", "example.test:8001"])
def test_websocket_url_rejects_other_schemes(base_url: str):
    with pytest.raises(ValueError, match="Invalid base URL"):
        AsyncTestClient(base_url)._websocket_url("/ws")
