"""Startup failure handling and WebSocketConfig pass-through."""

import socket
from contextlib import asynccontextmanager, suppress

import pytest
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosedError
from websockets.protocol import State

import fastapi_testing.async_fastapi_testing as module
from fastapi_testing import (
    AsyncTestServer,
    Config,
    ServerStartupError,
    WebSocketConfig,
    create_test_server,
    get_config,
    set_config,
)


@asynccontextmanager
async def failing_lifespan(app: FastAPI):
    raise RuntimeError("lifespan startup failed")
    yield


class TestStartupFailure:
    """uvicorn calls sys.exit() when startup fails; that must surface as an exception in the test."""

    async def test_lifespan_failure_raises_and_releases_port(self):
        server = AsyncTestServer(lifespan=failing_lifespan, startup_timeout=10.0)
        with pytest.raises(ServerStartupError, match="uvicorn exit code"):
            await server.start()

        assert server._server_task is None
        assert server._port is None
        assert module._port_generator.used_ports == set()

        await server.stop()  # still safe after a failed start

    async def test_bind_failure_raises_and_releases_port(self, monkeypatch):
        with socket.socket() as blocker:
            blocker.bind(("127.0.0.1", 0))
            blocker.listen()
            taken = blocker.getsockname()[1]
            monkeypatch.setattr(module._port_generator, "get_port", lambda: taken)

            server = AsyncTestServer(startup_timeout=10.0)
            with pytest.raises(ServerStartupError):
                await server.start()

        assert server._server_task is None
        assert server._port is None

    async def test_server_can_start_after_a_failed_start(self):
        with pytest.raises(ServerStartupError):
            async with create_test_server(lifespan=failing_lifespan):
                pass

        async with create_test_server() as server:

            @server.app.get("/ok")
            async def ok():
                return {"ok": True}

            response = await server.client.get("/ok")
            await response.expect_status(200)

    async def test_startup_error_is_a_runtime_error(self):
        # Existing callers catching RuntimeError keep working.
        assert issubclass(ServerStartupError, RuntimeError)


class _FakeConnection:
    state = State.CLOSED


class TestWebSocketConfigPassThrough:
    @pytest.fixture
    def captured(self, monkeypatch):
        calls: list[dict] = []

        async def fake_connect(url, **kwargs):
            calls.append(kwargs)
            return _FakeConnection()

        monkeypatch.setattr(module, "connect", fake_connect)
        return calls

    @pytest.fixture
    def restore_config(self):
        original = get_config()
        yield
        set_config(original)

    async def test_max_size_and_max_queue_from_config_are_used(self, captured):
        async with create_test_server() as server:
            await server.client.websocket("/ws", WebSocketConfig(max_size=1234, max_queue=5))
        assert captured[0]["max_size"] == 1234
        assert captured[0]["max_queue"] == 5

    async def test_unset_fields_use_active_global_config(self, captured, restore_config):
        set_config(Config(ws_max_message_size=4321, ws_max_queue_size=3))
        async with create_test_server() as server:
            await server.client.websocket("/ws", WebSocketConfig())
        assert captured[0]["max_size"] == 4321
        assert captured[0]["max_queue"] == 3
        assert "ping_interval" not in captured[0]

    async def test_zero_values_are_passed_through(self, captured):
        async with create_test_server() as server:
            await server.client.websocket("/ws", WebSocketConfig(ping_timeout=0, max_queue=0))
        assert captured[0]["ping_timeout"] == 0
        assert captured[0]["max_queue"] == 0


class TestWebSocketConfigReal:
    async def test_max_size_is_enforced(self):
        async with create_test_server() as server:

            @server.app.websocket("/big")
            async def big(ws: WebSocket):
                await ws.accept()
                try:
                    await ws.send_text("x" * 100)
                    await ws.receive_text()
                except WebSocketDisconnect:
                    pass

            ws_response = await server.client.websocket("/big", WebSocketConfig(max_size=16))
            with pytest.raises(ConnectionClosedError):
                await server.client.ws.receive_text(ws_response)

    async def test_options_can_disable_pings(self):
        async with create_test_server() as server:

            @server.app.websocket("/ws")
            async def ws_endpoint(ws: WebSocket):
                await ws.accept()
                with suppress(WebSocketDisconnect):
                    await ws.receive_text()

            ws_response = await server.client.websocket("/ws", options={"ping_interval": None})
            assert ws_response.websocket().ping_interval is None

            default_response = await server.client.websocket("/ws", WebSocketConfig(ping_interval=5.0))
            assert default_response.websocket().ping_interval == 5.0
