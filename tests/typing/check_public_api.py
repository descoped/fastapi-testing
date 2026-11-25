"""Static type checks for the public API, verified by pyright and ty (not collected by pytest).

Run: `pyright` and `uv run ty check`. Each `assert_type` must hold, and each
`pyright: ignore[...]` / `ty: ignore[...]` marks an error that MUST occur: pyright's
reportUnnecessaryTypeIgnoreComment and ty's unused-ignore-comment fail the check if a
deliberately wrong usage stops being an error.
"""

# pyright: reportUnnecessaryTypeIgnoreComment=true

import socket
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, assert_type

import httpx
from fastapi import FastAPI
from websockets.asyncio.client import ClientConnection

from fastapi_testing import (
    AsyncTestClient,
    AsyncTestResponse,
    AsyncTestServer,
    Config,
    PortGenerator,
    ShutdownMode,
    WebSocketConfig,
    WebSocketHelper,
    create_test_server,
    get_config,
    global_config,
)


class MyServer(AsyncTestServer):
    """A project's own server class, e.g. a fixture helper."""

    def seed(self) -> int:
        return 1


class MyClient(AsyncTestClient):
    pass


class MyResponse(AsyncTestResponse):
    pass


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    yield


async def server_types() -> None:
    async with create_test_server(lifespan=lifespan, startup_timeout=5.0) as server:
        assert_type(server, AsyncTestServer)
        assert_type(server.app, FastAPI)
        assert_type(server.base_url, str)
        assert_type(server.client, AsyncTestClient)

    async with AsyncTestServer() as server:
        assert_type(server, AsyncTestServer)

    # A subclass keeps its own type, and its methods, inside `async with`
    async with MyServer() as own:
        assert_type(own, MyServer)
        assert_type(own.seed(), int)
    async with MyClient("http://127.0.0.1:8001") as own_client:
        assert_type(own_client, MyClient)

    async with create_test_server(app=FastAPI(), client_timeout=5.0) as server:
        assert_type(server.app, FastAPI)

    mode: ShutdownMode = "graceful"
    async with create_test_server(shutdown=mode) as server:
        assert_type(server, AsyncTestServer)
    assert_type(Config().SHUTDOWN, ShutdownMode)

    # The shutdown mode is one of the two names
    AsyncTestServer(shutdown="slow")  # pyright: ignore[reportArgumentType]  # ty: ignore[invalid-argument-type]

    # app and client_timeout are keyword-only
    AsyncTestServer(None, 30.0, 10.0, FastAPI())  # pyright: ignore[reportCallIssue]  # ty: ignore[too-many-positional-arguments]

    # The lifespan must be a lifespan, not an arbitrary callable
    create_test_server(lifespan="startup")  # pyright: ignore[reportArgumentType]  # ty: ignore[invalid-argument-type]


async def client_types(client: AsyncTestClient) -> None:
    response = await client.get("/")
    assert_type(response, AsyncTestResponse)
    assert_type(response.status_code, int)
    assert_type(response.headers, httpx.Headers)
    assert_type(response.httpx_response, httpx.Response)
    assert_type(await response.text(), str)
    assert_type(await response.json(), Any)
    assert_type(await response.expect_status(200), AsyncTestResponse)

    async with client.stream("GET", "/events") as streamed:
        assert_type(streamed, httpx.Response)

    ws = await client.websocket("/ws", WebSocketConfig(subprotocols=["chat"]))
    assert_type(ws.websocket(), ClientConnection)
    assert_type(await WebSocketHelper.receive_text(ws), str)
    assert_type(await WebSocketHelper.receive_binary(ws), bytes)
    assert_type(await WebSocketHelper.drain_messages(ws), list[Any])

    # The status code is an int
    await response.expect_status("200")  # pyright: ignore[reportArgumentType]  # ty: ignore[invalid-argument-type]


async def response_subclass_types(response: MyResponse) -> None:
    assert_type(await response.expect_status(200), MyResponse)


def config_types() -> None:
    assert_type(get_config(), Config)
    assert_type(global_config, Config)
    assert_type(Config.from_env(), Config)
    assert_type(Config.from_file("settings.toml"), Config)

    class MyConfig(Config):
        pass

    assert_type(MyConfig.from_env(), MyConfig)
    assert_type(MyConfig.from_file("settings.toml"), MyConfig)

    ports = PortGenerator()
    assert_type(ports.get_port(), int)
    assert_type(ports.bind("127.0.0.1"), socket.socket)
