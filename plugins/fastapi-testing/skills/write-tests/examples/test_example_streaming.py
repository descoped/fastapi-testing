"""Server-Sent Events and WebSocket tests over real connections."""

from fastapi_testing import AsyncTestServer, WebSocketConfig


async def test_server_sent_events(server: AsyncTestServer):
    events: list[str] = []
    async with server.client.stream("GET", "/events", params={"count": 3}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                events.append(line.removeprefix("data: "))
    assert events == ["tick 0", "tick 1", "tick 2"]


async def test_leaving_an_endless_stream_early(server: AsyncTestServer):
    # Breaking out closes the response, which the app sees as the client disconnecting.
    async with server.client.stream("GET", "/events", params={"count": 1000}) as response:
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                break


async def test_websocket_echo(server: AsyncTestServer):
    ws = await server.client.websocket("/ws/echo")
    try:
        await server.client.ws.send_json(ws, {"n": 1})
        assert await server.client.ws.receive_json(ws) == {"echo": {"n": 1}}
    finally:
        await ws.websocket().close()


async def test_websocket_with_options(server: AsyncTestServer):
    config = WebSocketConfig(extra_headers={"X-Client": "tests"}, timeout=5.0)
    ws = await server.client.websocket("/ws/echo", config)
    try:
        await server.client.ws.send_json(ws, {"hello": "world"})
        await server.client.ws.expect_message(ws, expected={"echo": {"hello": "world"}}, timeout=1.0)
    finally:
        await ws.websocket().close()
