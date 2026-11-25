"""stop() in "fast" mode (the default) and "graceful" mode.

Fast skips uvicorn's graceful drain, which spends about 200 ms in fixed polling
sleeps on every stop, but must still release everything: open connections are
closed as a client disconnect, requests still running are cancelled after a
short grace, and the lifespan shutdown runs.
"""

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager

import anyio
import httpx
import pytest
import websockets
from fastapi import FastAPI, WebSocket
from fastapi.responses import StreamingResponse

from fastapi_testing import AsyncTestServer, Config, create_test_server, get_config, set_config


@pytest.fixture
def config() -> Iterator[None]:
    original = get_config()
    try:
        yield
    finally:
        set_config(original)


def _port_refuses_connections(port: int) -> bool:
    import socket

    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) != 0


@pytest.fixture
def uvicorn_errors(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """What uvicorn logs as application errors, including a cancelled request.

    uvicorn reconfigures its loggers (dictConfig) when a server is created, which
    drops handlers added earlier and stops propagation to caplog. Wrapping the
    logger's ``error`` method survives that.
    """
    messages: list[str] = []
    logger = logging.getLogger("uvicorn.error")
    original = logger.error

    def error(msg: object, *args: object, **kwargs: object) -> None:
        if "Exception in ASGI application" in str(msg):
            messages.append(str(msg))
        original(msg, *args, **kwargs)  # pyright: ignore[reportArgumentType]  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(logger, "error", error)
    return messages


async def test_fast_is_the_default_and_stop_is_quick():
    assert get_config().SHUTDOWN == "fast"
    server = AsyncTestServer()
    await server.start()
    assert (await server.client.get("/missing")).status_code == 404
    began = time.monotonic()
    await server.stop()
    assert time.monotonic() - began < 0.05


@pytest.mark.parametrize("shielded", [False, True])
async def test_open_stream_ends_as_a_disconnect(uvicorn_errors: list[str], shielded: bool):
    """An SSE stream open at stop ends exactly as when its client disconnects.

    Starlette cancels a streaming response when its client disconnects, so
    cleanup in the generator is cut short unless it shields itself; that holds
    for a real browser too, and in graceful mode. Shielded cleanup (careful code
    returning a database connection, say) completes within ``request_grace``.
    """
    cleanup: list[str] = []
    server = AsyncTestServer()
    await server.start()

    @server.app.get("/events")
    async def events():
        async def stream():
            try:
                while True:
                    yield b"data: tick\n\n"
                    await asyncio.sleep(0.05)
            finally:
                cleanup.append("started")
                if shielded:
                    with anyio.CancelScope(shield=True):
                        await asyncio.sleep(0.1)  # e.g. returning a connection to the pool
                    cleanup.append("finished")

        return StreamingResponse(stream(), media_type="text/event-stream")

    received = asyncio.Event()

    async def consume():
        async with httpx.AsyncClient(base_url=server.base_url) as client, client.stream("GET", "/events") as response:
            async for _ in response.aiter_bytes():
                received.set()

    consumer = asyncio.create_task(consume())
    await asyncio.wait_for(received.wait(), timeout=5)
    port = server._port
    assert port is not None
    began = time.monotonic()
    await server.stop()
    assert time.monotonic() - began < server.request_grace + 0.3
    assert cleanup == (["started", "finished"] if shielded else ["started"])
    assert _port_refuses_connections(port)
    assert server._requests == set()
    assert uvicorn_errors == []
    consumer.cancel()
    await asyncio.gather(consumer, return_exceptions=True)


async def test_in_flight_request_is_cancelled_quietly_after_the_grace(uvicorn_errors: list[str]):
    cancelled = asyncio.Event()
    server = AsyncTestServer()
    await server.start()

    @server.app.get("/slow")
    async def slow():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {}

    async with httpx.AsyncClient(base_url=server.base_url) as client:
        request = asyncio.create_task(client.get("/slow"))
        await asyncio.sleep(0.2)
        began = time.monotonic()
        await server.stop()
        assert time.monotonic() - began < server.request_grace + 0.3
        assert cancelled.is_set()
        with pytest.raises(httpx.TransportError):
            await request
    assert server._requests == set()
    assert uvicorn_errors == []


async def test_open_websocket_is_closed():
    server = AsyncTestServer()
    await server.start()

    @server.app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        while True:
            await websocket.receive_text()

    connection = await websockets.connect(server.base_url.replace("http", "ws") + "/ws")
    began = time.monotonic()
    await server.stop()
    assert time.monotonic() - began < server.request_grace + 0.3
    await asyncio.wait_for(connection.wait_closed(), timeout=2)
    assert server._requests == set()


async def test_lifespan_shutdown_still_runs():
    events: list[str] = []

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        events.append("startup")
        yield
        await asyncio.sleep(0.05)  # e.g. closing a pool
        events.append("shutdown")

    async with create_test_server(lifespan=lifespan):
        pass
    assert events == ["startup", "shutdown"]


async def test_graceful_lets_an_in_flight_request_finish():
    server = AsyncTestServer(shutdown="graceful", shutdown_timeout=5)
    await server.start()

    started = asyncio.Event()

    @server.app.get("/slow")
    async def slow():
        started.set()
        await asyncio.sleep(0.5)
        return {"done": True}

    async with httpx.AsyncClient(base_url=server.base_url) as client:
        request = asyncio.create_task(client.get("/slow"))
        await asyncio.wait_for(started.wait(), timeout=5)
        await server.stop()
        response = await request
    assert response.json() == {"done": True}


async def test_the_server_setting_overrides_the_config(config: None):
    set_config(Config(shutdown="graceful"))
    assert AsyncTestServer()._shutdown_mode() == "graceful"
    assert AsyncTestServer(shutdown="fast")._shutdown_mode() == "fast"
    with pytest.raises(ValueError, match="shutdown must be one of"):
        AsyncTestServer(shutdown="slow")  # pyright: ignore[reportArgumentType]  # ty: ignore[invalid-argument-type]


async def test_a_server_restarts_after_a_fast_stop():
    server = AsyncTestServer()
    for _ in range(3):
        await server.start()
        assert (await server.client.get("/missing")).status_code == 404
        await server.stop()


async def test_many_fast_cycles_leave_nothing_behind():
    """Fire-and-forget at scale: every cycle releases its socket, port, tasks and signal handlers."""
    import signal

    from fastapi_testing.async_fastapi_testing import _port_generator, _signal_handlers

    handler = signal.getsignal(signal.SIGINT)
    tasks_before = len(asyncio.all_tasks())
    for _ in range(50):
        async with create_test_server() as server:
            await server.client.get("/missing")
    assert len(asyncio.all_tasks()) == tasks_before
    assert _port_generator.used_ports == set()
    assert _signal_handlers._running == []
    assert signal.getsignal(signal.SIGINT) is handler


async def test_a_real_handler_error_is_still_logged(uvicorn_errors: list[str]):
    """The quiet cancellation is only for a fast stop; it also proves uvicorn_errors captures."""

    async def boom(scope, receive, send):
        raise RuntimeError("handler failed")

    server = AsyncTestServer()
    server.app = boom  # pyright: ignore[reportAttributeAccessIssue]  # ty: ignore[invalid-assignment]  # a bare ASGI app on purpose
    await server.start()
    assert (await server.client.get("/")).status_code == 500
    await server.stop()
    assert len(uvicorn_errors) == 1
