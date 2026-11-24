"""Server options: serving your own app, a client timeout of its own, strict environment config."""

import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI, Request

from fastapi_testing import AsyncTestServer, Config, create_test_server


def _create_app() -> FastAPI:
    """An application the way a project builds it, with its own lifespan."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[dict[str, str]]:
        yield {"source": "own lifespan"}

    app = FastAPI(lifespan=lifespan)

    @app.get("/source")
    async def source(request: Request):
        return {"source": request.state.source}

    return app


@pytest.mark.asyncio
async def test_create_test_server_serves_the_given_app_with_its_lifespan():
    async with create_test_server(app=_create_app()) as server:
        response = await server.client.get("/source")
        assert await response.json() == {"source": "own lifespan"}


@pytest.mark.asyncio
async def test_server_serves_the_given_app():
    app = _create_app()
    async with AsyncTestServer(app=app) as server:
        assert server.app is app
        assert (await server.client.get("/source")).status_code == 200


def test_app_and_lifespan_together_is_an_error():
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        yield

    with pytest.raises(ValueError, match="either app or lifespan"):
        AsyncTestServer(lifespan=lifespan, app=FastAPI())


@pytest.mark.asyncio
async def test_client_timeout_is_independent_of_startup_timeout():
    async with create_test_server(startup_timeout=60.0, client_timeout=0.3) as server:

        @server.app.get("/slow")
        async def slow():
            await asyncio.sleep(2)
            return {}

        began = time.monotonic()
        with pytest.raises(httpx.ReadTimeout):
            await server.client.get("/slow")
        assert time.monotonic() - began < 1.5


@pytest.mark.asyncio
async def test_client_timeout_defaults_to_30_seconds_whatever_the_startup_timeout():
    async with create_test_server(startup_timeout=5.0) as server:
        assert server.client._timeout == 30.0


class TestFromEnvValidation:
    ENV = {
        "FASTAPI_TESTING_PORT_RANGE_STAR": "9100",  # a typo
        "FASTAPI_TESTING_WS_RETRY_ATTEMPTS": "five",
        "FASTAPI_TESTING_WS_RETRY_DELAY": "0.5",
    }

    def test_default_ignores_bad_variables_and_warns(self, caplog: pytest.LogCaptureFixture):
        with patch.dict("os.environ", self.ENV, clear=True), caplog.at_level(logging.WARNING):
            config = Config.from_env()
        assert config.WS_RETRY_DELAY == 0.5
        assert config.WS_RETRY_ATTEMPTS == 3
        assert config.PORT_RANGE_START == 8001
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("FASTAPI_TESTING_PORT_RANGE_STAR: unknown setting" in w for w in warnings), warnings
        assert any("FASTAPI_TESTING_WS_RETRY_ATTEMPTS: must be an integer" in w for w in warnings), warnings

    def test_strict_raises_naming_every_problem(self):
        with patch.dict("os.environ", self.ENV, clear=True), pytest.raises(ValueError) as info:
            Config.from_env(strict=True)
        message = str(info.value)
        assert "FASTAPI_TESTING_PORT_RANGE_STAR: unknown setting" in message
        assert "FASTAPI_TESTING_WS_RETRY_ATTEMPTS: must be an integer, got 'five'" in message

    def test_strict_accepts_valid_variables(self):
        with patch.dict("os.environ", {"FASTAPI_TESTING_WS_RETRY_DELAY": "0.5"}, clear=True):
            assert Config.from_env(strict=True).WS_RETRY_DELAY == 0.5
