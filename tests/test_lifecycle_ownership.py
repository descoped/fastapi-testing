"""The server owns its lifecycle: socket, application lifespan and shutdown order.

Every test runs a real server. The suite runs with ``-W error`` in CI, so a
leaked socket (ResourceWarning) or an unretrieved task exception fails here.
"""

import asyncio
import socket
import time
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import StreamingResponse

import fastapi_testing.async_fastapi_testing as module
from fastapi_testing import AsyncTestServer, ServerShutdownError, ServerStartupError, create_test_server


def _pending_tasks() -> set[asyncio.Task]:
    return {t for t in asyncio.all_tasks() if not t.done() and t is not asyncio.current_task()}


def _port_refuses_connections(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return False
    except OSError:
        return True


class TestOwnedSocket:
    async def test_socket_is_closed_and_port_released_after_stop(self):
        server = AsyncTestServer()
        await server.start()
        port = server._port
        assert port is not None and not _port_refuses_connections(port)

        await server.stop()

        assert _port_refuses_connections(port)
        assert port not in module._port_generator.used_ports
        assert server._socket is None

    async def test_concurrent_servers_get_distinct_ports(self):
        servers = [AsyncTestServer() for _ in range(8)]
        await asyncio.gather(*(s.start() for s in servers))
        try:
            assert len({s._port for s in servers}) == len(servers)
        finally:
            await asyncio.gather(*(s.stop() for s in servers))
        assert module._port_generator.used_ports == set()


class TestOwnedLifespan:
    async def test_replaced_app_runs_its_lifespan(self):
        """Factflow's pattern: build the production app and assign it before start()."""
        events: list[str] = []

        @asynccontextmanager
        async def app_lifespan(app: FastAPI):
            events.append("startup")
            app.state.ready = True
            yield
            events.append("shutdown")

        app = FastAPI(lifespan=app_lifespan)

        @app.get("/ready")
        async def ready(request: Request):
            return {"ready": request.app.state.ready}

        server = AsyncTestServer()
        server.app = app
        await server.start()
        try:
            response = await server.client.get("/ready")
            assert await response.json() == {"ready": True}
            assert events == ["startup"]
        finally:
            await server.stop()
        assert events == ["startup", "shutdown"]

    async def test_lifespan_state_reaches_http_and_websocket_requests(self):
        @asynccontextmanager
        async def stateful(app: FastAPI):
            yield {"greeting": "hello"}

        async with create_test_server(lifespan=stateful) as server:

            @server.app.get("/state")
            async def state(request: Request):
                return {"greeting": request.state.greeting}

            @server.app.websocket("/ws")
            async def ws(websocket: WebSocket):
                await websocket.accept()
                await websocket.send_text(websocket.state.greeting)
                await websocket.close()

            response = await server.client.get("/state")
            assert await response.json() == {"greeting": "hello"}

            ws_response = await server.client.websocket("/ws")
            assert await server.client.ws.receive_text(ws_response) == "hello"

    async def test_middleware_sees_the_lifespan_scope(self):
        seen: list[str] = []
        app = FastAPI()

        class Recorder:
            def __init__(self, inner):
                self.inner = inner

            async def __call__(self, scope, receive, send):
                seen.append(scope["type"])
                await self.inner(scope, receive, send)

        app.add_middleware(Recorder)
        server = AsyncTestServer()
        server.app = app
        await server.start()
        await server.stop()
        assert "lifespan" in seen

    async def test_app_without_lifespan_support_still_serves(self):
        async def bare_asgi(scope, receive, send):
            if scope["type"] != "http":
                raise RuntimeError(f"unsupported scope {scope['type']}")
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"bare"})

        server = AsyncTestServer()
        server.app = bare_asgi  # type: ignore[assignment]
        await server.start()
        try:
            response = await server.client.get("/")
            assert await response.text() == "bare"
        finally:
            await server.stop()

    async def test_hung_startup_leaves_no_task_behind(self):
        release = asyncio.Event()

        @asynccontextmanager
        async def hung(app: FastAPI):
            await release.wait()
            yield

        before = _pending_tasks()
        server = AsyncTestServer(lifespan=hung, startup_timeout=0.5)
        began = time.monotonic()
        with pytest.raises(ServerStartupError, match="timed out"):
            await server.start()
        assert time.monotonic() - began < 3.0
        # The lifespan task is owned and cancelled: nothing is orphaned.
        assert _pending_tasks() - before == set()
        assert module._port_generator.used_ports == set()

    async def test_lifespan_shutdown_runs_after_a_failed_http_start(self, monkeypatch):
        events: list[str] = []

        @asynccontextmanager
        async def tracked(app: FastAPI):
            events.append("startup")
            yield
            events.append("shutdown")

        async def giving_up_startup(self, sockets=None):
            self.should_exit = True

        monkeypatch.setattr(module.uvicorn.Server, "startup", giving_up_startup)
        server = AsyncTestServer(lifespan=tracked, startup_timeout=5.0)
        with pytest.raises(ServerStartupError, match="exited during startup"):
            await server.start()
        assert events == ["startup", "shutdown"]
        assert server._socket is None and server._port is None


class TestShutdown:
    async def test_lifespan_shutdown_error_is_raised_after_cleanup(self):
        @asynccontextmanager
        async def failing_shutdown(app: FastAPI):
            yield
            raise ValueError("could not flush")

        server = AsyncTestServer(lifespan=failing_shutdown)
        await server.start()
        port = server._port
        assert port is not None

        with pytest.raises(ServerShutdownError, match="Application shutdown failed") as excinfo:
            await server.stop()

        assert isinstance(excinfo.value.__cause__, ValueError)
        assert _port_refuses_connections(port)
        assert port not in module._port_generator.used_ports
        await server.stop()  # nothing left to do, and no second error

    async def test_hung_lifespan_shutdown_times_out(self):
        @asynccontextmanager
        async def hung_shutdown(app: FastAPI):
            yield
            await asyncio.sleep(30)

        server = AsyncTestServer(lifespan=hung_shutdown, shutdown_timeout=0.5)
        await server.start()
        port = server._port
        began = time.monotonic()
        with pytest.raises(ServerShutdownError, match="did not complete"):
            await server.stop()
        assert time.monotonic() - began < 3.0
        assert port not in module._port_generator.used_ports

    async def test_http_stops_before_lifespan_shutdown(self):
        observed: dict[str, bool] = {}

        @asynccontextmanager
        async def checks_port(app: FastAPI):
            yield
            observed["refused_during_shutdown"] = _port_refuses_connections(app.state.port)

        server = AsyncTestServer(lifespan=checks_port)
        await server.start()
        server.app.state.port = server._port
        await server.stop()
        assert observed == {"refused_during_shutdown": True}

    async def test_open_stream_does_not_block_stop(self):
        async with create_test_server(shutdown_timeout=1.0) as server:

            @server.app.get("/forever")
            async def forever():
                async def events():
                    while True:
                        yield b"data: tick\n\n"
                        await asyncio.sleep(0.05)

                return StreamingResponse(events(), media_type="text/event-stream")

            port = server._port
            received = asyncio.Event()

            async def consume():
                async with (
                    httpx.AsyncClient(base_url=server.base_url) as client,
                    client.stream("GET", "/forever") as response,
                ):
                    async for _ in response.aiter_bytes():
                        received.set()

            consumer = asyncio.create_task(consume())
            await asyncio.wait_for(received.wait(), timeout=5.0)

            began = time.monotonic()
            await server.stop()
            # Graceful period (shutdown_timeout) plus uvicorn's polling, never the 30 s defaults.
            assert time.monotonic() - began < 5.0
            assert _port_refuses_connections(port)

            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

    async def test_cancelled_while_closing_client_still_releases_everything(self, monkeypatch):
        server = AsyncTestServer()
        await server.start()
        serve_task = server._server_task
        port = server._port
        assert serve_task is not None and port is not None

        async def slow_close():
            await asyncio.sleep(5)

        monkeypatch.setattr(server._client, "close", slow_close)
        stopping = asyncio.create_task(server.stop())
        await asyncio.sleep(0.1)
        stopping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopping

        await asyncio.sleep(0)  # let the cancelled serve task finish unwinding
        assert serve_task.done()
        assert _port_refuses_connections(port)
        assert port not in module._port_generator.used_ports

    async def test_externally_cancelled_serve_task_is_a_startup_error(self, monkeypatch):
        async def never_ready(self, sockets=None):
            await asyncio.sleep(30)

        monkeypatch.setattr(module.UvicornTestServer, "serve", never_ready)
        server = AsyncTestServer(startup_timeout=10.0)
        starting = asyncio.create_task(server.start())
        while server._server_task is None:
            await asyncio.sleep(0.01)
        server._server_task.cancel()
        with pytest.raises(ServerStartupError, match="exited during startup"):
            await starting
        assert server._port is None and server._socket is None
