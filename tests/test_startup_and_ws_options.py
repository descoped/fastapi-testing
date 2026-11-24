"""Startup failure handling and WebSocketConfig pass-through."""

import asyncio
import socket
import time
from contextlib import asynccontextmanager, suppress

import pytest
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from websockets.exceptions import ConnectionClosedError
from websockets.protocol import State

import fastapi_testing
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

    async def test_hung_lifespan_times_out_quickly_and_cleans_up(self):
        release = asyncio.Event()

        @asynccontextmanager
        async def hung_lifespan(app: FastAPI):
            await release.wait()
            yield

        server = AsyncTestServer(lifespan=hung_lifespan, startup_timeout=0.5, shutdown_timeout=10.0)
        began = time.monotonic()
        try:
            with pytest.raises(ServerStartupError, match="timed out"):
                await server.start()
            # Startup never completed, so stop() must not wait shutdown_timeout on top.
            assert time.monotonic() - began < 3.0
            assert server._server_task is None
            assert server._port is None
            assert module._port_generator.used_ports == set()
        finally:
            release.set()

    async def test_unexpected_serve_error_is_wrapped(self, monkeypatch):
        async def broken_serve(self, sockets=None):
            raise OSError("socket exploded")

        monkeypatch.setattr(module.UvicornTestServer, "serve", broken_serve)
        server = AsyncTestServer(startup_timeout=5.0)
        with pytest.raises(ServerStartupError, match="exited during startup") as excinfo:
            await server.start()
        assert isinstance(excinfo.value.__cause__, OSError)
        assert server._port is None

    async def test_startup_that_gives_up_is_not_reported_ready(self, monkeypatch):
        async def giving_up_startup(self, sockets=None):
            self.should_exit = True  # uvicorn abandoning startup without sys.exit()

        monkeypatch.setattr(module.uvicorn.Server, "startup", giving_up_startup)
        server = AsyncTestServer(startup_timeout=5.0)
        with pytest.raises(ServerStartupError, match="exited during startup"):
            await server.start()
        assert server._client is None
        assert server._port is None

    async def test_cancelling_stop_propagates_and_releases_port(self):
        @asynccontextmanager
        async def slow_shutdown(app: FastAPI):
            yield
            await asyncio.sleep(5)

        server = AsyncTestServer(lifespan=slow_shutdown, shutdown_timeout=10.0)
        await server.start()
        stopping = asyncio.create_task(server.stop())
        await asyncio.sleep(0.2)
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping
        assert server._port is None
        assert server._server_task is None

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


class TestPackageGlobalConfig:
    def test_package_global_config_follows_set_config(self):
        original = get_config()
        try:
            replacement = Config(ws_retry_attempts=11)
            set_config(replacement)
            assert fastapi_testing.global_config is replacement
            from fastapi_testing import global_config

            assert global_config is replacement
        finally:
            set_config(original)

    def test_unknown_package_attribute_raises(self):
        with pytest.raises(AttributeError, match="no_such_name"):
            _ = fastapi_testing.no_such_name
