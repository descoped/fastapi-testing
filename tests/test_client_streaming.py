"""The server's shared client, streaming requests and the underlying httpx response."""

import asyncio

import httpx
import pytest
from fastapi import WebSocket
from fastapi.responses import StreamingResponse

from fastapi_testing import AsyncTestClient, InvalidResponseTypeError, create_test_server


class TestSharedClient:
    async def test_async_with_server_client_does_not_close_it(self):
        async with create_test_server() as server:

            @server.app.get("/ping")
            async def ping():
                return {"ok": True}

            async with server.client as client:
                await (await client.get("/ping")).expect_status(200)
            # Still usable, also in a second block.
            await (await server.client.get("/ping")).expect_status(200)
            async with server.client as client:
                await (await client.get("/ping")).expect_status(200)

    async def test_stop_closes_the_shared_client(self):
        async with create_test_server() as server:
            shared = server.client
            async with shared:
                pass
        with pytest.raises(RuntimeError):
            await shared.get("/")

    async def test_own_client_closes_on_exit(self):
        async with create_test_server() as server:
            async with AsyncTestClient(server.base_url) as client:
                pass
            with pytest.raises(RuntimeError):
                await client.get("/")


class TestStreaming:
    async def test_stream_reads_server_sent_events(self):
        async with create_test_server() as server:

            @server.app.get("/events")
            async def events():
                async def generate():
                    for i in range(3):
                        yield f"data: {i}\n\n"
                        await asyncio.sleep(0.01)

                return StreamingResponse(generate(), media_type="text/event-stream")

            async with server.client.stream("GET", "/events") as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                data = [line async for line in response.aiter_lines() if line.startswith("data:")]

            assert data == ["data: 0", "data: 1", "data: 2"]

    async def test_stream_can_stop_early_on_an_endless_stream(self):
        async with create_test_server(shutdown_timeout=1.0) as server:

            @server.app.get("/forever")
            async def forever():
                async def generate():
                    while True:
                        yield "data: tick\n\n"
                        await asyncio.sleep(0.01)

                return StreamingResponse(generate(), media_type="text/event-stream")

            async with server.client.stream("GET", "/forever") as response:
                async for line in response.aiter_lines():
                    if line == "data: tick":
                        break
            assert response.is_closed


class TestHttpxResponse:
    async def test_httpx_response_is_exposed(self):
        async with create_test_server() as server:

            @server.app.get("/item")
            async def item():
                return {"id": 1}

            response = await server.client.get("/item")
            assert isinstance(response.httpx_response, httpx.Response)
            assert response.httpx_response.json() == {"id": 1}

    async def test_httpx_response_rejects_websocket(self):
        async with create_test_server() as server:

            @server.app.websocket("/ws")
            async def ws(websocket: WebSocket):
                await websocket.accept()
                await websocket.receive_text()

            ws_response = await server.client.websocket("/ws")
            with pytest.raises(InvalidResponseTypeError):
                _ = ws_response.httpx_response
