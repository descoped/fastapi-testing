"""Fixtures for testing example_app with fastapi-testing.

Every fixture is function-scoped, so each test gets its own event loop, server
and resources (pytest-asyncio's default). Each fixture closes what it opens.
Because ``server`` depends on ``store`` and ``rates_server``, pytest stops the
server first and closes its resources after it.

pyproject.toml:

    [tool.pytest.ini_options]
    asyncio_mode = "auto"
    asyncio_default_fixture_loop_scope = "function"
"""

from collections.abc import AsyncGenerator

import pytest
from example_app import Settings, Store, create_app, get_settings, get_store
from fastapi.responses import JSONResponse

from fastapi_testing import AsyncTestServer, create_test_server


@pytest.fixture
async def store() -> AsyncGenerator[Store]:
    """A resource the test owns, like a database pool: opened and closed by this fixture."""
    store = Store()
    try:
        yield store
    finally:
        await store.close()


@pytest.fixture
async def rates_server() -> AsyncGenerator[AsyncTestServer]:
    """A fake of the upstream rates API: a second real server the application calls over HTTP."""
    async with create_test_server() as upstream:

        @upstream.app.get("/rates/{currency}")
        async def rate(currency: str):
            if currency == "XXX":
                return JSONResponse({"error": "unknown currency"}, status_code=404)
            return {"currency": currency, "rate": 2.0}

        yield upstream


@pytest.fixture
async def server(store: Store, rates_server: AsyncTestServer) -> AsyncGenerator[AsyncTestServer]:
    """The real application, with its dependencies pointed at test doubles."""
    app = create_app()
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_settings] = lambda: Settings(rates_url=rates_server.base_url)
    async with create_test_server(app=app) as server:
        yield server
