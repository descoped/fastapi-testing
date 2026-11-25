"""The application's own lifespan, startup failures, and choosing a shutdown mode."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
import pytest
from example_app import create_app
from fastapi import FastAPI

from fastapi_testing import ServerStartupError, create_test_server


async def test_the_apps_own_lifespan_runs_without_overrides():
    # Without overrides, get_store reads the Store the app's lifespan put in lifespan state.
    async with create_test_server(app=create_app()) as server:
        await (await server.client.post("/items/a", json={"price": 1})).expect_status(201)
        await (await server.client.get("/items/a")).expect_status(200)


async def test_a_failing_startup_raises_with_the_apps_error():
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        raise RuntimeError("database unreachable")
        yield

    with pytest.raises(ServerStartupError) as info:
        async with create_test_server(lifespan=lifespan):
            pass
    assert isinstance(info.value.__cause__, RuntimeError)


async def test_graceful_shutdown_lets_an_in_flight_request_finish():
    # The default "fast" stop cancels requests still running after 0.5 s. Use "graceful"
    # only for a test about shutdown behaviour itself.
    async with create_test_server(shutdown="graceful", shutdown_timeout=5) as server:

        @server.app.get("/slow")
        async def slow():
            await asyncio.sleep(0.3)
            return {"done": True}

        async with httpx.AsyncClient(base_url=server.base_url) as client:
            request = asyncio.create_task(client.get("/slow"))
            await asyncio.sleep(0.1)
            await server.stop()  # stop while the request is running
            assert (await request).json() == {"done": True}
